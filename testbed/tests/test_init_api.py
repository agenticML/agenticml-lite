"""User-facing singleton API and its on-disk results."""
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import time

import agenticml
from agenticml import Run
from agenticml.store import RunReader, list_runs
import pytest


def start(**kwargs):
    options = dict(project="api", name="baseline", system_metrics=False, auto_track=False,
                   capture_env=False, track_source=False)
    options.update(kwargs)
    return agenticml.init(**options)


def reader(run):
    return RunReader(run.root, run.run_id)


def test_init_metadata_verbose_and_root(tmp_path, capsys):
    root = tmp_path / "explicit-root"
    run = start(dir=root, config={"lr": 0.2}, tags=["baseline"], notes="hello",
                group="group-a", job_type="training", verbose=True)
    assert agenticml.get_active_run() is run
    assert re.fullmatch(r"\d{8}T\d{6}-[0-9a-f]{8}", run.run_id)
    output = capsys.readouterr().out
    assert run.run_id in output and str(root / "runs" / run.run_id) in output
    assert "agenticml ui" in output
    meta_path = root / "runs" / run.run_id / "meta.json"
    meta = json.loads(meta_path.read_text())
    assert meta == reader(run).meta()
    assert meta["project"] == "api" and meta["name"] == "baseline"
    assert meta["config"] == {"lr": 0.2} and meta["tags"] == ["baseline"]
    assert meta["notes"] == "hello" and meta["group"] == "group-a"
    assert meta["job_type"] == "training" and meta["format_version"] == 1
    assert meta["status"] == "running" and meta["finished_at"] is None
    assert meta["pid"] == os.getpid() and meta["created_at"].endswith("Z")
    assert not Path(os.environ["AGENTICML_DIR"]).exists()
    agenticml.finish()
    assert agenticml.get_active_run() is None
    final = reader(run).meta()
    assert final["status"] == "finished" and final["finished_at"] >= meta["created_at"]
    assert list_runs(root)[0]["run_id"] == run.run_id
    second = start()
    assert second.root == Path(os.environ["AGENTICML_DIR"])


def test_steps_merge_pending_and_finish():
    run = start()
    agenticml.log({"loss": 2}, step=4)
    agenticml.log({"lr": 0.1}, commit=False)
    agenticml.log({"loss": 1})
    agenticml.log({"loss": 0.5})
    agenticml.log({"pending": 3}, commit=False)
    agenticml.finish()
    rr = reader(run)
    assert rr.series("loss")["points"] == [[4, 2.0], [5, 1.0], [6, 0.5]]
    assert rr.series("lr")["points"][0][0] == 5
    assert rr.series("lr")["points"][0][1] == pytest.approx(0.1)
    assert rr.series("pending")["points"] == [[7, 3.0]]
    assert rr.last_step() == 7
    assert rr.time_index() and all(t >= 0 for _, t in rr.time_index())
    assert rr.events()[-1]["kind"] == "finish"
    before = rr.events()
    run.finish()
    assert rr.events() == before


def test_explicit_step_change_commits_pending():
    run = start()
    agenticml.log({"a": 1}, step=2, commit=False)
    agenticml.log({"b": 2}, step=8)
    agenticml.log({"c": 3})
    agenticml.finish()
    assert reader(run).series("a")["points"] == [[2, 1.0]]
    assert reader(run).series("b")["points"] == [[8, 2.0]]
    assert reader(run).series("c")["points"] == [[9, 3.0]]


def test_non_numeric_and_nested_metrics(caplog):
    run = start()
    agenticml.log({"nested": {"loss": 2}, "text": "hello", "array": [1, 2],
                   "none": None, "numeric-string": "2.5", "boolean": True,
                   "finite": 3, "nan": float("nan"), "inf": float("inf")})
    with pytest.raises(TypeError, match="dict"):
        agenticml.log([1])
    agenticml.finish()
    rr = reader(run)
    assert set(rr.keys()) == {"boolean", "finite", "nan", "inf"}
    assert "numeric-string" not in rr.keys()
    assert rr.series("boolean")["points"] == [[0, 1.0]]
    assert math.isnan(rr.series("nan")["points"][0][1])
    assert math.isinf(rr.series("inf")["points"][0][1])
    assert "dropping scalar" in caplog.text
    with pytest.raises(KeyError):
        rr.series("nested")


def test_config_summary_proxy_and_persistence():
    run = start(config={"lr": 0.1, "remove": True})
    agenticml.config.update({"epochs": 3}, batch_size=8)
    agenticml.config["nested"] = {"x": [1, 2]}
    del agenticml.config["remove"]
    assert "remove" not in agenticml.config
    assert len(agenticml.config) == 4
    assert agenticml.config.get("missing", 42) == 42
    assert set(agenticml.config.keys()) == set(iter(agenticml.config))
    assert dict(agenticml.config.items()) == dict(agenticml.config)
    assert list(agenticml.config.values()) == list(run.config.values())
    agenticml.summary["final_loss"] = 0.25
    agenticml.summary.update(best=0.9, nested={"x": 1}, nonfinite=float("nan"))
    agenticml.summary["remove"] = 1
    del agenticml.summary["remove"]
    expected = dict(agenticml.config)
    assert "final_loss" in repr(agenticml.summary)
    agenticml.finish()
    assert reader(run).meta()["config"] == expected
    assert reader(run).meta()["summary"] == {"final_loss": 0.25, "best": 0.9,
                                             "nested": {"x": 1}, "nonfinite": None}
    assert any(e["kind"] == "config_update" for e in reader(run).events())
    with pytest.raises(RuntimeError, match="before agenticml.init"):
        agenticml.config.get("lr")


def test_reinit_finishes_previous(caplog):
    first = start(name="first")
    agenticml.log({"x": 1})
    second = start(name="second")
    assert reader(first).meta()["status"] == "finished"
    assert reader(first).series("x")["points"] == [[0, 1.0]]
    assert agenticml.get_active_run() is second
    assert "finishing previous" in caplog.text


@pytest.mark.parametrize("source", ["argument", "environment", "rank"])
def test_disabled_writes_nothing(source, tmp_path, monkeypatch, capsys):
    options = {"verbose": True}
    if source == "argument":
        options["mode"] = "disabled"
    elif source == "environment":
        monkeypatch.setenv("AGENTICML_MODE", "disabled")
    else:
        monkeypatch.setenv("RANK", "1")
    run = start(**options)
    assert run.run_id is None
    agenticml.log({"anything": object()})
    assert agenticml.log_artifact(tmp_path / "does-not-exist", copy=True) is None
    agenticml.track_files(tmp_path / "does-not-exist")
    for method in (agenticml.track_model, agenticml.track_optimizer,
                   agenticml.track_loss, agenticml.track_dataloader):
        method(object())
    agenticml.config["x"] = 1
    agenticml.summary["x"] = 1
    agenticml.finish()
    assert not Path(os.environ["AGENTICML_DIR"]).exists()
    assert not (Path.home() / ".agenticml").exists()
    assert not (Path.home() / ".config/agenticml").exists()
    if source != "rank":
        assert "tracking disabled" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["offline", "invalid-mode"])
def test_mode_alias_and_fallback(mode, caplog):
    run = start(mode=mode)
    assert run.mode == "local"
    agenticml.finish()
    assert reader(run).meta()["status"] == "finished"
    if mode == "invalid-mode":
        assert "falling back to 'local'" in caplog.text


def test_mode_argument_wins_over_environment(monkeypatch):
    monkeypatch.setenv("AGENTICML_MODE", "disabled")
    run = start(mode="local")
    assert run.run_id is not None


@pytest.mark.parametrize("env,enabled", [
    ({"RANK": "0", "LOCAL_RANK": "1"}, True),
    ({"LOCAL_RANK": "1"}, False),
    ({"RANK": "not-an-integer", "LOCAL_RANK": "1"}, False),
    ({"RANK": "not-an-integer", "PMI_RANK": "0", "LOCAL_RANK": "1"}, True),
    ({"RANK": "not-an-integer", "PMI_RANK": "1", "LOCAL_RANK": "0"}, False),
])
def test_rank_precedence(env, enabled, monkeypatch):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    run = start()
    agenticml.log({"user/loss": 2})
    agenticml.finish()
    if enabled:
        assert reader(run).series("user/loss")["points"] == [[0, 2.0]]
    else:
        assert run.run_id is None
        assert not Path(os.environ["AGENTICML_DIR"]).exists()


def test_system_metrics_do_not_advance_user_steps():
    pytest.importorskip("psutil")
    run = start(system_metrics=True, system_metrics_interval=0.02, flush_interval=0.01)
    expected = {"_system/cpu.percent", "_system/memory.percent", "_system/disk.percent"}
    try:
        agenticml.log({"user/loss": 2})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if expected <= set(reader(run).keys()):
                break
            time.sleep(0.01)
        else:
            pytest.fail("system sampler did not persist CPU, memory and disk metrics")
        agenticml.log({"user/loss": 1})
    finally:
        agenticml.finish()
    rr = reader(run)
    assert rr.series("user/loss")["points"] == [[0, 2.0], [1, 1.0]]
    for key in expected:
        points = rr.series(key)["points"]
        assert points and all(step in (0, 1) and 0 <= value <= 100 for step, value in points)

    disabled = start(system_metrics=False, system_metrics_interval=0.02)
    agenticml.log({"user/loss": 3})
    time.sleep(0.06)  # More than two sampler intervals if the off switch regresses.
    agenticml.finish()
    assert not any(k.startswith("_system/") for k in reader(disabled).keys())


@pytest.mark.parametrize("cached_bytes,warns", [(11, True), (9, False)])
def test_store_budget_warns_without_deleting(cached_bytes, warns, tmp_path, monkeypatch, caplog):
    import importlib
    from agenticml.store import RunWriter, put_bytes
    run_module = importlib.import_module("agenticml.run")
    monkeypatch.setattr(run_module, "_budget_warned", False)  # Warning is once per process.
    monkeypatch.setenv("AGENTICML_MAX_BYTES", "10")
    root = tmp_path / "budget-store"
    old = RunWriter.create(root, meta={"project": "budget", "name": "preserved"})
    old.write_scalars([(0, {"loss": 1})])
    old.finish("finished")
    put_bytes(root, b"preserve orphan objects too")
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and p.name != "usage.json"}
    (root / "usage.json").write_text(json.dumps({"bytes": cached_bytes, "computed_at": time.time(), "runs": {}}))
    caplog.clear()
    run = start(dir=root)
    assert run.run_id is not None
    assert ("exceeds budget" in caplog.text) is warns
    agenticml.finish()
    assert all(path.is_file() and path.read_bytes() == data for path, data in before.items())
    assert len(list_runs(root)) == 2


@pytest.mark.parametrize("resume,error,message", [
    ("never", ValueError, "resume='never'"),
    ("must", FileNotFoundError, "missing"),
])
def test_resume_rejection(resume, error, message, caplog):
    # init deliberately catches constructor errors and disables tracking.
    run = start(id="missing", resume=resume)
    assert run.run_id is None
    assert "could not initialize tracking" in caplog.text
    with pytest.raises(error, match=message):
        Run("api", "n", id="missing", resume=resume, system_metrics=False,
            track_source=False, capture_env=False, auto_track=False)


def test_resume_allow_and_must_continue_steps():
    run = start(id="resume-test", resume="allow", config={"old": 1}, tags=["saved"])
    agenticml.log({"loss": 2}, step=5)
    agenticml.summary["best"] = 2
    agenticml.finish()
    created = reader(run).meta()["created_at"]
    reopened = start(project="ignored", name="ignored", id=run.run_id, resume="allow", config={"new": 2})
    assert reopened.project == "api" and reopened.name == "baseline"
    assert reopened.tags == ["saved"] and reopened.summary["best"] == 2
    assert reopened.config == {"old": 1, "new": 2}
    assert reader(reopened).meta()["finished_at"] is None
    agenticml.log({"loss": 1})
    agenticml.finish()
    third = start(id=run.run_id, resume="must")
    agenticml.log({"loss": 0.5})
    agenticml.finish()
    rr = reader(third)
    assert rr.series("loss")["points"] == [[5, 2.0], [6, 1.0], [7, 0.5]]
    assert rr.meta()["created_at"] == created
    assert sum(e["kind"] == "resume" for e in rr.events()) == 2


@pytest.mark.parametrize("source", ["path", "bytes", "pil", "file"])
def test_image_sources(source, tmp_path):
    from PIL import Image
    image = Image.new("RGB", (12, 8), "red")
    path = tmp_path / "image.png"
    image.save(path)
    payload = path.read_bytes()
    values = {"path": path, "bytes": payload, "pil": image, "file": io.BytesIO(payload)}
    run = start()
    agenticml.log({"preview": agenticml.Image(values[source], caption="sample")}, step=3)
    agenticml.finish()
    rr = reader(run)
    row, = rr.media("preview")
    assert row["step"] == 3 and row["caption"] == "sample" and row["id"] == 0
    assert row["content_type"] == "image/png"
    assert row["sha256"] == hashlib.sha256(payload).hexdigest()
    assert (run.root / "objects" / row["sha256"][:2] / row["sha256"]).read_bytes() == payload
    assert rr.media() == {"preview": [row]} and rr.media("unknown") == []
    assert rr.keys() == []


def test_artifacts_and_code_snapshot(tmp_path):
    path = tmp_path / "checkpoint.json"
    path.write_text('{"weight": 1}\n')
    source = tmp_path / "user_model.py"
    source.write_text("def forward(x):\n    return x * 2\n")
    run = start(track_source=True)
    agenticml.track_files(source)
    reference = agenticml.log_artifact(path, name="model", metadata={"epoch": 1})
    copied = agenticml.log_artifact(path, name="model", copy=True)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert reference["sha256"] is None and reference["copy"] is False
    assert copied["sha256"] == digest and copied["copy"] is True
    assert (reference["version"], copied["version"]) == (1, 2)
    agenticml.finish()
    path.write_text("changed")
    assert (run.root / "objects" / digest[:2] / digest).read_text() == '{"weight": 1}\n'
    rr = reader(run)
    assert [a["version"] for a in rr.artifacts()] == [1, 2]
    assert rr.artifacts()[0]["path"] == str(path.resolve())
    assert rr.artifacts()[0]["metadata"] == {"epoch": 1}
    entries = rr.code_manifest()
    assert any(e["path"].endswith("user_model.py") for e in entries)
    assert digest in rr.referenced_shas()
    assert {"code", "artifact", "finish"} <= {e["kind"] for e in rr.events()}


@pytest.mark.parametrize("kind", ["model", "optimizer", "loss", "dataloader"])
def test_plain_object_tracking(kind):
    run = start()
    getattr(agenticml, "track_" + kind)(object(), extra_metadata={"label": "plain"})
    agenticml.finish()
    rr = reader(run)
    entry, = [e for e in rr.code_manifest() if e["path"] == f"agenticml/{kind}.json"]
    payload = json.loads((run.root / "objects" / entry["sha256"][:2] / entry["sha256"]).read_text())
    assert payload["name"] == "default" and payload["label"] == "plain"
    assert rr.meta()["status"] == "finished"


def test_calls_without_init_and_finish_idempotent():
    agenticml.finish()
    agenticml.finish()
    for call in (lambda: agenticml.log({"x": 1}), lambda: agenticml.log_artifact("missing"),
                 lambda: agenticml.track_model(object())):
        with pytest.raises(RuntimeError, match="before agenticml.init"):
            call()


def test_non_main_process_override_and_parent(monkeypatch):
    monkeypatch.setenv("RANK", "1")
    run = start(only_main_process=False, parent_run_id="parent-run")
    agenticml.log({"loss": 1})
    agenticml.finish()
    assert reader(run).meta()["parent_run_id"] == "parent-run"
    assert reader(run).series("loss")["points"] == [[0, 1.0]]


def test_context_exception_marks_crashed():
    with pytest.raises(RuntimeError, match="training failed"):
        with start() as run:
            agenticml.log({"loss": 1})
            raise RuntimeError("training failed")
    assert reader(run).meta()["status"] == "crashed"
    assert reader(run).series("loss")["points"] == [[0, 1.0]]


@pytest.mark.parametrize("bad_step", [True, -1, 2**32, 1.5])
def test_invalid_scalar_steps_are_dropped(bad_step, caplog):
    run = start()
    agenticml.log({"bad": 1}, step=bad_step)
    agenticml.log({"valid": 0.125}, step=0)
    agenticml.finish()
    assert reader(run).keys() == ["valid"]
    assert reader(run).series("valid")["points"] == [[0, 0.125]]
    assert "dropping scalar" in caplog.text


def test_media_retention_resize_and_dedup(monkeypatch):
    from PIL import Image
    monkeypatch.setenv("AGENTICML_MEDIA_MAX_PER_KEY", "4")
    monkeypatch.setenv("AGENTICML_MEDIA_MAX_PX", "10")
    image = Image.new("RGB", (40, 20), "blue")
    run = start()
    for step in range(6):
        agenticml.log({"preview": agenticml.Image(image)}, step=step)
    agenticml.finish()
    rows = reader(run).media("preview")
    assert len(rows) <= 4 and rows[0]["step"] == 0 and rows[-1]["step"] == 5
    assert len({r["sha256"] for r in rows}) == 1
    assert [r["id"] for r in rows] == sorted({r["id"] for r in rows})
    row = rows[0]
    path = run.root / "objects" / row["sha256"][:2] / row["sha256"]
    with Image.open(path) as resized:
        assert resized.size == (10, 5)
    reopened = start(id=run.run_id, resume="must")
    agenticml.log({"preview": agenticml.Image(image)}, step=6)
    agenticml.finish()
    assert reader(reopened).media("preview")[-1]["id"] == 6


def test_code_snapshot_file_size_cap(tmp_path):
    small = tmp_path / "small.py"
    big = tmp_path / "oversized.py"
    small.write_text("x = 1\n")
    big.write_text("# large file\n" * 100)
    run = start(track_source=True, max_file_bytes=32, max_total_bytes=1024)
    agenticml.track_files(small, big)
    agenticml.finish()
    manifest = reader(run).code_manifest()
    assert any(e["path"].endswith("small.py") for e in manifest)
    assert not any(e["path"].endswith("oversized.py") for e in manifest)

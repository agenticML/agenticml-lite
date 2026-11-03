"""Local list, show, export, and dashboard command dispatch."""

import json
import os
import sys
import time
from datetime import datetime

import pytest

from agenticml import cli
from agenticml.store import OVERVIEW_MIN_POINTS, RunWriter, list_runs


def test_ls_lists_newest_first_filters_and_handles_empty_runs(tmp_path, capsys):
    first = RunWriter.create(tmp_path, run_id="first", meta={"project": "p", "name": "one"})
    first.write_scalars([(10, {"loss": 1})])
    first.write_scalars([(2, {"loss": 2})])
    first.finish("finished")
    second = RunWriter.create(tmp_path, run_id="second", meta={"project": "q", "name": "two"})
    stale = time.time() - 121
    os.utime(second.run_dir / "meta.json", (stale, stale))
    assert cli.main(["ls", "--dir", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith("second") and "running" in lines[1] and lines[1].endswith("-")
    assert lines[2].startswith("first") and "finished" in lines[2] and lines[2].endswith("2")
    assert lines[-1] == "2 runs"
    cli.main(["ls", "--dir", str(tmp_path), "--project", "p"])
    filtered = capsys.readouterr().out
    assert "first" in filtered and "second" not in filtered and "1 run" in filtered
    second.finish("finished")


def test_ls_json_is_list_runs_metadata(tmp_path, monkeypatch, capsys):
    writer = RunWriter.create(tmp_path, meta={"project": "p"})
    writer.finish("finished")
    monkeypatch.setenv("AGENTICML_DIR", str(tmp_path))
    assert cli.main(["ls", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == list_runs(tmp_path)
    cli.main(["ls", "--project", "absent", "--json"])
    assert json.loads(capsys.readouterr().out) == []


def test_ls_empty_root(tmp_path, capsys):
    assert cli.main(["ls", "--dir", str(tmp_path)]) == 0
    assert capsys.readouterr().out.endswith("0 runs\n")


def test_ui_dispatch_defaults_and_options(tmp_path, monkeypatch):
    from agenticml.ui import server

    calls = []
    monkeypatch.setenv("AGENTICML_DIR", str(tmp_path))
    monkeypatch.setattr(server, "serve", lambda *args: calls.append(args))
    cli.main(["ui"])
    cli.main(["ui", "--dir", str(tmp_path / "other"), "--host", "0.0.0.0", "--port", "8123", "--open"])
    cli.main(["ui", "--read-only"])
    assert calls == [(tmp_path, "127.0.0.1", 6006, False, False),
                     (tmp_path / "other", "0.0.0.0", 8123, True, False),
                     (tmp_path, "127.0.0.1", 6006, False, True)]


def test_ui_bind_errors_are_one_line(tmp_path, capsys):
    from agenticml.ui import server

    held = server.make_server(tmp_path, "127.0.0.1", 0)
    port = held.server_address[1]
    try:
        code = cli.main(["ui", "--dir", str(tmp_path), "--port", str(port)])
    finally:
        held.server_close()
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("agenticml ui:")
    assert "Traceback" not in captured.err
    assert captured.err.count("\n") == 1

    code = cli.main(["ui", "--dir", str(tmp_path), "--port", "70000"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("agenticml ui:")
    assert "Traceback" not in captured.err
    assert captured.err.count("\n") == 1

    code = cli.main(["ui", "--dir", str(tmp_path), "--host", "not a host"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("agenticml ui:")
    assert "Traceback" not in captured.err
    assert captured.err.count("\n") == 1


def test_ui_ctrl_c_prints_stopped(tmp_path, monkeypatch, capsys):
    from agenticml.ui import server

    def serve_forever(self):
        raise KeyboardInterrupt()

    monkeypatch.setattr(server.DashboardServer, "serve_forever", serve_forever)
    assert cli.main(["ui", "--dir", str(tmp_path), "--port", "0"]) == 0
    captured = capsys.readouterr()
    assert "stopped" in captured.out
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_du_table_and_json(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="du-run", meta={"project": "proj", "name": "train"})
    writer.write_scalars([(0, {"loss": 0.5})])
    writer.add_media("preview", 0, b"fake-png", "image/png", None)
    writer.finish("finished")

    # Table output
    assert cli.main(["du", "--dir", str(tmp_path)]) == 0
    captured = capsys.readouterr().out
    assert "RUN ID" in captured
    assert "PROJECT/NAME" in captured
    assert "STATUS" in captured
    assert "SYNCED?" in captured
    assert "RAW" in captured
    assert "OVERVIEW" in captured
    assert "MEDIA" in captured
    assert "TOTAL" in captured
    assert "du-run" in captured
    assert "proj/train" in captured
    assert "Runs dir total:" in captured
    assert "Objects dir total:" in captured
    assert "Total store usage:" in captured

    # JSON output
    assert cli.main(["du", "--dir", str(tmp_path), "--json"]) == 0
    raw_json = capsys.readouterr().out
    data = json.loads(raw_json)
    assert "runs" in data
    assert "runs_total" in data
    assert "objects_total" in data
    assert "unreferenced_total" in data
    assert len(data["runs"]) == 1
    assert data["runs"][0]["run_id"] == "du-run"


def _ls_cells(header, line):
    titles = ("RUN ID", "PROJECT", "NAME", "STATUS", "CREATED", "LAST STEP")
    starts = [header.index(title) for title in titles]
    padded = line.ljust(len(header))
    cells = {}
    for index, title in enumerate(titles):
        end = starts[index + 1] if index + 1 < len(starts) else len(padded)
        cells[title] = padded[starts[index]:end].strip()
    return cells


def test_ls_missing_name_prints_dash_not_none(tmp_path, capsys):
    none_name = RunWriter.create(tmp_path, run_id="none-name", meta={"project": "p", "name": None})
    none_name.finish("finished")
    blank = RunWriter.create(tmp_path, run_id="blank-name", meta={"project": "p", "name": ""})
    blank.finish("finished")
    omitted = RunWriter.create(tmp_path, run_id="omitted-name", meta={"project": "q"})
    omitted.finish("finished")
    named = RunWriter.create(tmp_path, run_id="named", meta={"project": "p", "name": "kept"})
    named.finish("finished")
    literal = RunWriter.create(tmp_path, run_id="literal-none", meta={"project": None, "name": "None"})
    literal.finish("finished")

    assert cli.main(["ls", "--dir", str(tmp_path)]) == 0
    lines = capsys.readouterr().out.splitlines()
    header, *body = lines
    assert body[-1] == "5 runs"
    rows = {_ls_cells(header, line)["RUN ID"]: _ls_cells(header, line) for line in body[:-1]}
    for run_id in ("none-name", "blank-name", "omitted-name"):
        assert rows[run_id]["NAME"] == "-"
        assert rows[run_id]["NAME"] != "None"
    assert rows["named"]["NAME"] == "kept"
    assert rows["none-name"]["PROJECT"] == "p"
    assert rows["literal-none"]["NAME"] == "None"
    assert rows["literal-none"]["PROJECT"] == ""

    assert cli.main(["ls", "--dir", str(tmp_path), "--json"]) == 0
    payload = {row["run_id"]: row for row in json.loads(capsys.readouterr().out)}
    assert payload["none-name"]["name"] is None
    assert payload["named"]["name"] == "kept"
    assert payload["literal-none"]["name"] == "None"

    assert cli.main(["du", "--dir", str(tmp_path)]) == 0
    usage = capsys.readouterr().out
    assert "p/kept" in usage
    assert "None/None" not in usage and "p/None" not in usage


def test_verbose_init_and_sync_label_omit_literal_none(tmp_path, monkeypatch, capsys):
    import agenticml
    from agenticml.store import RunReader
    from agenticml.sync import SyncResult, _run_label, sync_run

    assert _run_label({"project": "demo", "name": "train"}) == "demo/train"
    assert _run_label({"project": 67890, "name": 12345}) == "67890/12345"
    assert _run_label({"project": "p", "name": None}) == "p/-"
    assert _run_label({"project": "p", "name": ""}) == "p/-"
    assert _run_label({"project": "p"}) == "p/-"
    assert _run_label({"project": None, "name": "n"}) == "/n"
    assert "None" not in _run_label({"project": None, "name": None})

    writer = RunWriter.create(tmp_path, run_id="sync-none", meta={"project": "p", "name": None})
    writer.finish("finished")
    result = SyncResult(writer.run_id)
    sync_run(RunReader(tmp_path, writer.run_id), "http://127.0.0.1", None, result, dry_run=True)
    assert result.label == "p/-"
    assert "None" not in result.line()

    monkeypatch.delenv("AGENTICML_MODE", raising=False)
    monkeypatch.delenv("AGENTICML_DIR", raising=False)
    monkeypatch.delenv("AGENTICML_PROJECT", raising=False)
    monkeypatch.delenv("AGENTICML_NAME", raising=False)
    monkeypatch.delattr(sys.modules["__main__"], "__file__", raising=False)
    run = agenticml.init(
        project="p",
        name=None,
        mode="local",
        dir=tmp_path,
        verbose=True,
        track_source=False,
        capture_env=False,
        system_metrics=False,
        only_main_process=False,
        auto_track=False,
    )
    try:
        out = capsys.readouterr().out
        assert "project=p" in out
        assert "name=-" in out
        assert "name=None" not in out
        assert run.name is None
    finally:
        agenticml.finish()


def test_gc_cli(tmp_path, monkeypatch, capsys):
    writer = RunWriter.create(tmp_path, run_id="gc-run", meta={"project": "p", "name": "n"})
    writer.write_scalars([(0, {"x": 1.0})])
    writer.finish("finished")

    # In tests sys.stdin.isatty() is False -> non-TTY without --yes must refuse with exit 2
    assert cli.main(["gc", "--dir", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "confirmation required on non-interactive terminal (use --yes)" in err

    # --dry-run succeeds without --yes
    assert cli.main(["gc", "--dir", str(tmp_path), "--dry-run", "--include-unsynced"]) == 0
    out = capsys.readouterr().out
    assert "Would delete" in out
    assert (tmp_path / "runs" / "gc-run").is_dir()

    # With --yes and --include-unsynced -> deletes
    assert cli.main(["gc", "--dir", str(tmp_path), "--yes", "--include-unsynced"]) == 0
    out = capsys.readouterr().out
    assert "Deleted 1 run" in out
    assert not (tmp_path / "runs" / "gc-run").exists()


def _reject_json_constant(token):
    raise ValueError(token)


def test_show_human_sections_and_metric_last(tmp_path, capsys):
    artifact = tmp_path / "model.pt"
    artifact.write_bytes(b"hello world!")
    writer = RunWriter.create(
        tmp_path,
        run_id="show-run",
        meta={
            "project": "demo",
            "name": "train",
            "tags": ["a", "b"],
            "group": "sweep",
            "job_type": "train",
            "notes": "first line\nsecond line",
            "task": "classify",
            "task_source": "explicit",
            "parent_run_id": "parent-1",
        },
    )
    writer.set_config({"lr": 0.5, "_argv": ["train.py"], "batch": 32})
    writer.set_summary({"best": 0.1})
    writer.write_scalars([(0, {"loss": 1.0, "acc": 0.1})])
    writer.write_scalars([(1, {"loss": 0.5, "acc": 0.5})])
    writer.write_scalars([(2, {"loss": 0.3333, "acc": 0.9})])
    writer.add_artifact(artifact, "checkpoint", "file", {})
    writer.add_media("preview", 0, b"fake-png", "image/png", None)
    writer.finish("finished", {"best": 0.1})

    meta = json.loads((tmp_path / "runs" / "show-run" / "meta.json").read_text(encoding="utf-8"))
    created = datetime.fromisoformat(
        str(meta["created_at"]).replace("Z", "+00:00"),
    ).astimezone().strftime("%Y-%m-%d %H:%M")

    assert cli.main(["show", "show-run", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "_argv" not in out
    assert "show-run" in out
    assert "demo" in out
    assert "train" in out
    assert "finished" in out
    assert "classify (explicit)" in out
    assert created in out
    assert "parent-1" in out
    assert "a, b" in out
    assert "sweep" in out
    assert "job type" in out
    notes_line = next(line for line in out.splitlines() if line.startswith("notes"))
    assert "first line …" in notes_line
    assert "second line" not in notes_line
    assert "lr = 0.5" in out
    assert "batch = 32" in out
    assert "best = 0.1" in out
    assert "metrics (2)" in out
    loss_line = next(line for line in out.splitlines() if line.strip().startswith("loss"))
    loss_cells = loss_line.split()
    assert loss_cells[:4] == ["loss", "0.3333", "2", "3"]
    acc_line = next(line for line in out.splitlines() if line.strip().startswith("acc"))
    acc_cells = acc_line.split()
    assert acc_cells[:4] == ["acc", "0.9", "2", "3"]
    assert "artifacts (1)" in out
    assert "checkpoint" in out
    assert "12 B" in out
    assert str(artifact.resolve()) in out
    assert "media (1)" in out
    assert "preview" in out
    assert "1 item(s)" in out


def test_show_omits_empty_sections(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="empty-run", meta={"project": "p"})
    writer.finish("finished")
    assert cli.main(["show", "empty-run", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "empty-run" in out
    assert "config" not in out
    assert "summary" not in out
    assert "metrics" not in out
    assert "artifacts" not in out
    assert "media" not in out


def test_show_json_metrics_and_nan_is_null(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="json-run", meta={"project": "p", "name": "n"})
    writer.write_scalars([(0, {"loss": 1.0})])
    writer.write_scalars([(1, {"loss": 0.5, "bad": float("nan")})])
    writer.finish("finished")
    assert cli.main(["show", "json-run", "--dir", str(tmp_path), "--json"]) == 0
    raw = capsys.readouterr().out
    payload = json.loads(raw, parse_constant=_reject_json_constant)
    assert payload["run_id"] == "json-run"
    assert payload["metrics"]["loss"] == {"last_step": 1, "last": 0.5, "count": 2}
    assert payload["metrics"]["bad"]["last_step"] == 1
    assert payload["metrics"]["bad"]["last"] is None
    assert payload["metrics"]["bad"]["count"] == 1
    assert payload["effective_status"] == "finished"
    assert "NaN" not in raw and "Infinity" not in raw


def test_show_and_export_missing_or_invalid_run(tmp_path, capsys):
    assert cli.main(["show", "missing-run", "--dir", str(tmp_path)]) == 1
    show_err = capsys.readouterr()
    assert show_err.out == ""
    assert "agenticml show: no run 'missing-run' in " in show_err.err
    assert cli.main(["export", "missing-run", "--dir", str(tmp_path)]) == 1
    export_err = capsys.readouterr()
    assert export_err.out == ""
    assert "agenticml export: no run 'missing-run' in " in export_err.err
    assert cli.main(["show", "bad/id", "--dir", str(tmp_path)]) == 1
    assert capsys.readouterr().err.startswith("agenticml show:")
    assert cli.main(["export", "bad/id", "--dir", str(tmp_path)]) == 1
    assert capsys.readouterr().err.startswith("agenticml export:")


def test_show_running_then_finished_status(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="live-run", meta={"project": "p"})
    assert cli.main(["show", "live-run", "--dir", str(tmp_path)]) == 0
    running = capsys.readouterr().out
    status_line = next(line for line in running.splitlines() if line.startswith("status"))
    assert status_line.split()[-1] == "running"
    writer.finish("finished")
    assert cli.main(["show", "live-run", "--dir", str(tmp_path)]) == 0
    finished = capsys.readouterr().out
    status_line = next(line for line in finished.splitlines() if line.startswith("status"))
    assert status_line.split()[-1] == "finished"


def test_show_pruned_series_uses_overview(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="pruned-run", meta={"project": "p"})
    count = OVERVIEW_MIN_POINTS + 1
    writer.write_scalars([(step, {"loss": float(step)}) for step in range(count)])
    writer.finish("finished")
    bin_path = writer.run_dir / "scalars" / "0.bin"
    assert bin_path.is_file()
    bin_path.unlink()
    assert cli.main(["show", "pruned-run", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    loss_line = next(line for line in out.splitlines() if line.strip().startswith("loss"))
    cells = loss_line.split()
    assert cells[0] == "loss"
    assert cells[2] == str(count - 1)
    assert cells[3] == str(count)
    assert cli.main(["export", "pruned-run", "--dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "raw points were pruned by sync --prune; skipped" in captured.err


def test_export_csv_default_key_subset_and_unknown(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="exp-run", meta={"project": "p"})
    writer.write_scalars([(0, {"loss": 1.0, "acc": 0.5})])
    writer.write_scalars([(1, {"loss": 0.25, "acc": 0.75})])
    writer.finish("finished")

    assert cli.main(["export", "exp-run", "--dir", str(tmp_path)]) == 0
    rows = capsys.readouterr().out.splitlines()
    assert rows[0] == "key,step,value"
    assert rows[1:] == [
        "loss,0,1.0",
        "loss,1,0.25",
        "acc,0,0.5",
        "acc,1,0.75",
    ]

    assert cli.main([
        "export", "exp-run", "--dir", str(tmp_path), "--key", "acc", "--key", "loss",
    ]) == 0
    subset = capsys.readouterr().out.splitlines()
    assert subset[0] == "key,step,value"
    assert subset[1:] == [
        "acc,0,0.5",
        "acc,1,0.75",
        "loss,0,1.0",
        "loss,1,0.25",
    ]

    assert cli.main(["export", "exp-run", "--dir", str(tmp_path), "--key", "nope"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "agenticml export: run exp-run has no metric 'nope'" in captured.err


def test_export_json_nan_is_null(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="exp-json", meta={"project": "p"})
    writer.write_scalars([(0, {"loss": float("nan")})])
    writer.finish("finished")
    assert cli.main(["export", "exp-json", "--dir", str(tmp_path), "--format", "json"]) == 0
    raw = capsys.readouterr().out
    payload = json.loads(raw, parse_constant=_reject_json_constant)
    assert payload["run_id"] == "exp-json"
    assert payload["series"]["loss"] == [[0, None]]
    assert "NaN" not in raw


def test_export_output_file_matches_stdout(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="exp-file", meta={"project": "p"})
    writer.write_scalars([(0, {"loss": 1.5})])
    writer.finish("finished")
    assert cli.main(["export", "exp-file", "--dir", str(tmp_path)]) == 0
    stdout = capsys.readouterr().out
    dest = tmp_path / "metrics.csv"
    assert cli.main(["export", "exp-file", "--dir", str(tmp_path), "-o", str(dest)]) == 0
    assert capsys.readouterr().out == ""
    assert dest.read_text(encoding="utf-8") == stdout


def test_export_preserves_out_of_order_file_order(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="exp-order", meta={"project": "p"})
    writer.write_scalars([(10, {"x": 1.0})])
    writer.write_scalars([(2, {"x": 2.0})])
    writer.finish("finished")
    assert cli.main(["export", "exp-order", "--dir", str(tmp_path)]) == 0
    rows = capsys.readouterr().out.splitlines()
    assert rows[1:] == ["x,10,1.0", "x,2,2.0"]


def test_show_odd_lineage_prints_dash_parent(tmp_path, capsys):
    for run_id, lineage in (("lin-list", ["x"]), ("lin-str", "s")):
        writer = RunWriter.create(tmp_path, run_id=run_id, meta={"project": "p"})
        writer.finish("finished")
        path = writer.run_dir / "meta.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        meta["lineage"] = lineage
        path.write_text(json.dumps(meta), encoding="utf-8")
        assert cli.main(["show", run_id, "--dir", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        parent_line = next(line for line in out.splitlines() if line.startswith("parent"))
        assert parent_line.split()[-1] == "-"


def test_show_last_is_file_tail_including_nan(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="tail-run", meta={"project": "p"})
    writer.write_scalars([(10, {"loss": 1.0})])
    writer.write_scalars([(2, {"loss": 2.0})])
    writer.write_scalars([(3, {"loss": 3.0})])
    writer.write_scalars([(4, {"loss": float("nan")})])
    writer.finish("finished")
    assert cli.main(["show", "tail-run", "--dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    loss_line = next(line for line in out.splitlines() if line.strip().startswith("loss"))
    assert loss_line.split()[:4] == ["loss", "nan", "4", "4"]


def test_show_json_recursion_is_one_line(tmp_path, monkeypatch, capsys):
    writer = RunWriter.create(tmp_path, run_id="deep-run", meta={"project": "p"})
    writer.finish("finished")

    def boom(value):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(cli, "_json_safe", boom)
    assert cli.main(["show", "deep-run", "--dir", str(tmp_path), "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("agenticml show:")
    assert "Traceback" not in captured.err
    assert captured.err.count("\n") == 1


def test_export_csv_nonfinite(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="exp-nonfinite", meta={"project": "p"})
    writer.write_scalars([
        (0, {"x": float("nan")}),
        (1, {"x": float("inf")}),
        (2, {"x": float("-inf")}),
    ])
    writer.finish("finished")
    assert cli.main(["export", "exp-nonfinite", "--dir", str(tmp_path)]) == 0
    rows = capsys.readouterr().out.splitlines()
    assert rows[1:] == ["x,0,nan", "x,1,inf", "x,2,-inf"]


def test_export_json_finite_structure(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="exp-json-fin", meta={"project": "p"})
    writer.write_scalars([(0, {"loss": 1.0}), (1, {"loss": 0.25, "acc": 0.5})])
    writer.finish("finished")
    assert cli.main(["export", "exp-json-fin", "--dir", str(tmp_path), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "run_id": "exp-json-fin",
        "series": {
            "loss": [[0, 1.0], [1, 0.25]],
            "acc": [[1, 0.5]],
        },
    }


def test_export_output_missing_directory(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="exp-miss-dir", meta={"project": "p"})
    writer.write_scalars([(0, {"loss": 1.0})])
    writer.finish("finished")
    dest = tmp_path / "nope" / "metrics.csv"
    assert cli.main(["export", "exp-miss-dir", "--dir", str(tmp_path), "-o", str(dest)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("agenticml export:")
    assert "Traceback" not in captured.err
    assert not dest.exists()
    assert not dest.parent.exists()


def test_export_all_pruned_does_not_create_output(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="exp-pruned-all", meta={"project": "p"})
    count = OVERVIEW_MIN_POINTS + 1
    writer.write_scalars([(step, {"loss": float(step)}) for step in range(count)])
    writer.finish("finished")
    (writer.run_dir / "scalars" / "0.bin").unlink()
    dest = tmp_path / "out.csv"
    assert cli.main(["export", "exp-pruned-all", "--dir", str(tmp_path), "-o", str(dest)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "raw points were pruned by sync --prune; skipped" in captured.err
    assert not dest.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_export_output_replaces_content_keeps_mode(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="exp-mode", meta={"project": "p"})
    writer.write_scalars([(0, {"loss": 1.0})])
    writer.finish("finished")
    dest = tmp_path / "out.csv"
    dest.write_text("old contents\n", encoding="utf-8")
    dest.chmod(0o640)
    assert cli.main(["export", "exp-mode", "--dir", str(tmp_path), "-o", str(dest)]) == 0
    assert capsys.readouterr().out == ""
    text = dest.read_text(encoding="utf-8")
    assert text.startswith("key,step,value")
    assert "old contents" not in text
    assert dest.stat().st_mode & 0o777 == 0o640


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")
def test_export_output_through_symlink_keeps_link(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="exp-link", meta={"project": "p"})
    writer.write_scalars([(0, {"loss": 1.0})])
    writer.finish("finished")
    target = tmp_path / "target.csv"
    target.write_text("old contents\n", encoding="utf-8")
    link = tmp_path / "link.csv"
    link.symlink_to(target)
    assert cli.main(["export", "exp-link", "--dir", str(tmp_path), "-o", str(link)]) == 0
    assert capsys.readouterr().out == ""
    assert link.is_symlink()
    assert os.path.samefile(link, target)
    text = target.read_text(encoding="utf-8")
    assert text.startswith("key,step,value")
    assert "old contents" not in text


def test_export_output_devnull(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="exp-null", meta={"project": "p"})
    writer.write_scalars([(0, {"loss": 1.0})])
    writer.finish("finished")
    assert cli.main(["export", "exp-null", "--dir", str(tmp_path), "-o", os.devnull]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_export_unknown_key_leaves_output_untouched(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="exp-keep", meta={"project": "p"})
    writer.write_scalars([(0, {"loss": 1.0})])
    writer.finish("finished")
    dest = tmp_path / "keep.csv"
    dest.write_text("keep-me\n", encoding="utf-8")
    assert cli.main([
        "export", "exp-keep", "--dir", str(tmp_path), "--key", "nope", "-o", str(dest),
    ]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "agenticml export: run exp-keep has no metric 'nope'" in captured.err
    assert dest.read_text(encoding="utf-8") == "keep-me\n"


def test_export_mixed_pruned_and_present_key(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="exp-mixed", meta={"project": "p"})
    count = OVERVIEW_MIN_POINTS + 1
    writer.write_scalars([(step, {"loss": float(step), "acc": 0.5}) for step in range(count)])
    writer.finish("finished")
    (writer.run_dir / "scalars" / "0.bin").unlink()
    assert cli.main([
        "export", "exp-mixed", "--dir", str(tmp_path), "--key", "loss", "--key", "acc",
    ]) == 0
    captured = capsys.readouterr()
    assert "loss: raw points were pruned by sync --prune; skipped" in captured.err
    rows = captured.out.splitlines()
    assert rows[0] == "key,step,value"
    assert all(row.startswith("acc,") for row in rows[1:])
    assert not any(row.startswith("loss,") for row in rows[1:])
    assert len(rows) == 1 + count


def test_show_and_export_corrupt_meta(tmp_path, capsys):
    writer = RunWriter.create(tmp_path, run_id="bad-meta", meta={"project": "p"})
    writer.write_scalars([(0, {"loss": 1.0})])
    writer.finish("finished")
    (writer.run_dir / "meta.json").write_text("{", encoding="utf-8")
    assert cli.main(["show", "bad-meta", "--dir", str(tmp_path)]) == 1
    show = capsys.readouterr()
    assert show.out == ""
    assert show.err.startswith("agenticml show:")
    assert "Traceback" not in show.err
    assert show.err.count("\n") == 1
    assert cli.main(["export", "bad-meta", "--dir", str(tmp_path)]) == 1
    export = capsys.readouterr()
    assert export.out == ""
    assert export.err.startswith("agenticml export:")
    assert "Traceback" not in export.err
    assert export.err.count("\n") == 1


def test_help_lists_show_and_export(capsys):
    try:
        cli.main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
    else:
        raise AssertionError("expected SystemExit")
    out = capsys.readouterr().out
    assert "show" in out
    assert "export" in out

